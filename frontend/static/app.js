const { createApp, computed, onMounted, reactive, ref, watch } = Vue;
const { createRouter, createWebHashHistory, useRoute, useRouter } = VueRouter;

const API_BASE = "http://127.0.0.1:8002";

const demoAccounts = [
  { username: "user101", password: "pass101", label: "用户 101", desc: "已有较多高分历史" },
  { username: "user202", password: "pass202", label: "用户 202", desc: "偏家庭与轻松内容" },
  { username: "user303", password: "pass303", label: "用户 303", desc: "偏剧情与爱情内容" },
  { username: "user000", password: "pass000", label: "新用户", desc: "0 条评分记录" }
];

const savedToken = localStorage.getItem("movie_rec_token") || "";
const savedUser = localStorage.getItem("movie_rec_user");

const appState = reactive({
  movies: [],
  totalMovieCount: 0,
  loaded: false,
  error: "",
  token: savedToken,
  user: savedUser ? JSON.parse(savedUser) : null
});

function safeText(value, fallback = "") {
  return value && String(value).trim() ? String(value).trim() : fallback;
}

function hasPoster(movie) {
  return !!safeText(movie.poster);
}

function formatMovieScore(score) {
  const value = Number(score);
  if (!Number.isFinite(value)) {
    return "--";
  }
  return value.toFixed(1);
}

function getMovieWeight(movie) {
  return (Number(movie.voteCount) || 0) * 0.002 + (Number(movie.popularity) || 0) * 0.35 + (Number(movie.score) || 0);
}

function getMovies() {
  return appState.movies;
}

function getMovieById(id) {
  return getMovies().find((movie) => String(movie.id) === String(id));
}

function searchMovies(query) {
  const keyword = query.trim().toLowerCase();
  const movies = getMovies();
  if (!keyword) {
    return movies;
  }
  return movies.filter((movie) => {
    const haystack = `${safeText(movie.title)} ${safeText(movie.originalTitle)} ${movie.genres.join(" ")} ${safeText(movie.overview)}`.toLowerCase();
    return haystack.includes(keyword);
  });
}

function getFeaturedMovies() {
  return getMovies()
    .filter((movie) => hasPoster(movie) && safeText(movie.overview))
    .sort((a, b) => getMovieWeight(b) - getMovieWeight(a))
    .slice(0, 8);
}

function getDefaultReason(movie) {
  const genres = Array.isArray(movie.genres) ? movie.genres.slice(0, 2) : [];
  if (genres.length > 0) {
    return `这部电影属于 ${genres.join(" / ")} 类型，整体热度和口碑都还不错。`;
  }
  return "这部电影在当前片库里的热度和口碑都不错，适合先加入候选。";
}

async function loadMovies() {
  try {
    const response = await fetch("./data/movies.json");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    appState.movies = Array.isArray(data) ? data : [];
    appState.loaded = true;
  } catch (error) {
    appState.error = `电影数据加载失败：${error.message}`;
    appState.loaded = true;
  }
}

async function loadCatalogStats() {
  try {
    const data = await apiFetch("/api/catalog/stats");
    appState.totalMovieCount = Number(data.total_movies) || 0;
  } catch (error) {
    appState.totalMovieCount = appState.movies.length;
  }
}

function setSession(token, user) {
  appState.token = token;
  appState.user = user;
  localStorage.setItem("movie_rec_token", token);
  localStorage.setItem("movie_rec_user", JSON.stringify(user));
}

function clearSession() {
  appState.token = "";
  appState.user = null;
  localStorage.removeItem("movie_rec_token");
  localStorage.removeItem("movie_rec_user");
}

async function apiFetch(path, options = {}) {
  const headers = {
    ...(options.headers || {})
  };

  if (appState.token) {
    headers.Authorization = `Bearer ${appState.token}`;
  }

  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers
  });

  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();

  if (!response.ok) {
    if (response.status === 401) {
      clearSession();
    }
    const detail = typeof data === "object" && data && data.detail ? data.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }

  return data;
}

async function validateSession() {
  if (!appState.token) {
    return;
  }
  try {
    const data = await apiFetch("/api/auth/me");
    if (data && data.user) {
      appState.user = data.user;
      localStorage.setItem("movie_rec_user", JSON.stringify(data.user));
    }
  } catch (error) {
    clearSession();
  }
}

async function fetchMyRating(movieId) {
  return apiFetch(`/api/users/me/ratings/${movieId}`);
}

async function saveMyRating(movieId, rating) {
  return apiFetch(`/api/users/me/ratings/${movieId}`, {
    method: "PUT",
    body: JSON.stringify({ rating })
  });
}

async function deleteMyRating(movieId) {
  return apiFetch(`/api/users/me/ratings/${movieId}`, {
    method: "DELETE"
  });
}

async function fetchRatingHistory(limit = 500) {
  return apiFetch(`/api/users/me/ratings?limit=${limit}`);
}

function buildRecommendationQuery(limit, excludeIds = [], extraParams = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (Array.isArray(excludeIds) && excludeIds.length > 0) {
    params.set("exclude_ids", excludeIds.join(","));
  }
  Object.entries(extraParams).forEach(([key, value]) => {
    if (value === undefined || value === null) {
      return;
    }
    params.set(key, String(value));
  });
  return params.toString();
}

async function fetchPopularRecommendations(limit = 20, excludeIds = []) {
  return apiFetch(`/api/recommendations/popular?${buildRecommendationQuery(limit, excludeIds)}`);
}

async function fetchMyRecommendations(limit = 20, options = {}) {
  const query = buildRecommendationQuery(limit, [], {
    next_batch: options.nextBatch ? "true" : "false"
  });
  return apiFetch(`/api/recommendations/me?${query}`);
}

function normalizeRecommendationMovie(movie) {
  return {
    id: movie.movie_id,
    title: movie.title,
    originalTitle: movie.original_title || "",
    overview: movie.overview || "",
    poster: movie.poster_url || "",
    genres: Array.isArray(movie.genres) ? movie.genres : [],
    year: movie.release_year || "",
    score: movie.vote_average ?? 0,
    voteCount: movie.vote_count ?? 0,
    popularity: movie.popularity ?? 0,
    reason: movie.reason || "",
    channel: movie.channel || "",
    sourceChannels: Array.isArray(movie.source_channels) ? movie.source_channels : []
  };
}

const MovieCard = {
  props: ["movie"],
  setup() {
    return { formatMovieScore };
  },
  template: `
    <router-link class="movie-card panel" :to="'/movie/' + movie.id">
      <img class="movie-poster" :src="movie.poster" :alt="movie.title" />
      <div class="movie-card-body">
        <div class="movie-card-top">
          <div>
            <h3 class="movie-title">{{ movie.title }}</h3>
            <p class="movie-original">{{ movie.originalTitle }}</p>
          </div>
          <span class="movie-score">TMDb {{ formatMovieScore(movie.score) }}/10</span>
        </div>
        <p class="movie-overview">{{ movie.overview }}</p>
        <p v-if="movie.reason" class="movie-reason">{{ movie.reason }}</p>
        <div class="movie-meta">
          <span v-for="genre in movie.genres.slice(0, 2)" :key="genre" class="meta-pill">{{ genre }}</span>
        </div>
      </div>
    </router-link>
  `
};

const HomeView = {
  components: { MovieCard },
  setup() {
    return {
      appState,
      featuredMovies: computed(() => getFeaturedMovies())
    };
  },
  template: `
    <section class="section-shell">
      <div v-if="!appState.loaded" class="panel panel-inner">正在加载电影数据...</div>
      <div v-else-if="appState.error" class="panel panel-inner">{{ appState.error }}</div>
      <template v-else>
        <div class="hero-card panel">
          <div class="hero-copy">
            <span class="eyebrow">今日片单</span>
            <h1 class="title">今晚想看点什么？</h1>
            <p class="subtitle">这里已经接通了电影库、评分系统和推荐链路。你现在可以登录、打分、查看历史，也可以直接体验多路召回推荐。</p>
            <div class="toolbar">
              <router-link class="button-primary" to="/recommend">推荐流程页</router-link>
              <router-link class="button-secondary" to="/recommend-final">最终推荐页</router-link>
              <router-link class="button-secondary" to="/search">搜索电影</router-link>
            </div>
          </div>
          <div class="hero-stats">
            <div class="stat-card">
              <div class="stat-label">当前阶段</div>
              <div class="stat-value">推荐链路已打通</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">已收录电影</div>
              <div class="stat-value">{{ appState.movies.length }} 部</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">下一步</div>
              <div class="stat-value">工业化完善</div>
            </div>
          </div>
        </div>

        <div class="section-heading">
          <div>
            <p class="eyebrow">精选</p>
            <h2 class="small-title">先看这几部</h2>
          </div>
        </div>
        <div class="movie-grid">
          <MovieCard v-for="movie in featuredMovies" :key="movie.id" :movie="movie" />
        </div>
      </template>
    </section>
  `
};

const SearchView = {
  components: { MovieCard },
  setup() {
    const keyword = ref("");
    const currentPage = ref(1);
    const pageSize = 24;
    const results = computed(() => searchMovies(keyword.value));
    const totalPages = computed(() => {
      const count = Math.ceil(results.value.length / pageSize);
      return count > 0 ? count : 1;
    });
    const paginatedResults = computed(() => {
      const start = (currentPage.value - 1) * pageSize;
      return results.value.slice(start, start + pageSize);
    });

    watch(keyword, () => {
      currentPage.value = 1;
    });

    watch(results, () => {
      if (currentPage.value > totalPages.value) {
        currentPage.value = totalPages.value;
      }
    });

    function goPrevPage() {
      if (currentPage.value > 1) {
        currentPage.value -= 1;
      }
    }

    function goNextPage() {
      if (currentPage.value < totalPages.value) {
        currentPage.value += 1;
      }
    }

    const resultSummary = computed(() => {
      const totalCount = appState.totalMovieCount || appState.movies.length;
      if (!keyword.value.trim()) {
        return `当前片库共 ${totalCount} 部电影`;
      }
      return `当前筛到 ${results.value.length} 部电影，片库共 ${totalCount} 部`;
    });

    return {
      appState,
      keyword,
      results,
      resultSummary,
      currentPage,
      totalPages,
      paginatedResults,
      goPrevPage,
      goNextPage
    };
  },
  template: `
    <section class="section-shell">
      <div v-if="!appState.loaded" class="panel panel-inner">正在加载电影数据...</div>
      <div v-else-if="appState.error" class="panel panel-inner">{{ appState.error }}</div>
      <template v-else>
        <div class="panel panel-inner stack">
          <div>
            <p class="eyebrow">搜索</p>
            <h1 class="small-title">找一部你想看的电影</h1>
          </div>
          <input v-model="keyword" class="search-input" placeholder="输入片名、英文名、类型或简介关键词" />
          <div class="search-toolbar">
            <div class="result-count">{{ resultSummary }}</div>
            <div v-if="results.length > 0" class="page-indicator">第 {{ currentPage }} / {{ totalPages }} 页</div>
          </div>
        </div>

        <div v-if="results.length === 0" class="panel panel-inner empty-state">
          <p class="subtitle">没有找到匹配的电影。</p>
        </div>
        <template v-else>
          <div class="movie-grid">
            <MovieCard v-for="movie in paginatedResults" :key="movie.id" :movie="movie" />
          </div>
          <div class="pagination-bar">
            <button class="button-secondary" @click="goPrevPage" :disabled="currentPage === 1">上一页</button>
            <span class="pagination-text">当前第 {{ currentPage }} 页，共 {{ totalPages }} 页</span>
            <button class="button-secondary" @click="goNextPage" :disabled="currentPage === totalPages">下一页</button>
          </div>
        </template>
      </template>
    </section>
  `
};

const LoginView = {
  setup() {
    const router = useRouter();
    const username = ref("");
    const password = ref("");
    const submitting = ref(false);
    const error = ref("");

    async function submitLogin() {
      submitting.value = true;
      error.value = "";
      try {
        const data = await apiFetch("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({
            username: username.value,
            password: password.value
          })
        });
        setSession(data.token, data.user);
        router.push("/history");
      } catch (err) {
        error.value = err.message;
      } finally {
        submitting.value = false;
      }
    }

    function useDemo(account) {
      username.value = account.username;
      password.value = account.password;
    }

    return {
      appState,
      demoAccounts,
      username,
      password,
      error,
      submitting,
      submitLogin,
      useDemo
    };
  },
  template: `
    <section class="section-shell narrow-shell">
      <div class="panel panel-inner auth-panel">
        <div class="stack">
          <div>
            <p class="eyebrow">登录</p>
            <h1 class="small-title">进入你的观影账户</h1>
          </div>
          <div v-if="appState.user" class="success-box">
            当前已登录：{{ appState.user.display_name }}（{{ appState.user.username }}）
          </div>
          <div class="form-grid">
            <label class="field-label">用户名</label>
            <input v-model="username" class="search-input" placeholder="请输入用户名" />
            <label class="field-label">密码</label>
            <input v-model="password" type="password" class="search-input" placeholder="请输入密码" />
          </div>
          <div v-if="error" class="error-box">{{ error }}</div>
          <button class="button-primary full-button" @click="submitLogin" :disabled="submitting">
            {{ submitting ? '登录中...' : '登录' }}
          </button>
        </div>
      </div>

      <div class="panel panel-inner">
        <div class="stack">
          <div>
            <p class="eyebrow">示例账号</p>
            <h2 class="small-title">点一下就能填入</h2>
          </div>
          <div class="demo-grid">
            <button
              v-for="account in demoAccounts"
              :key="account.username"
              class="demo-card"
              @click="useDemo(account)"
            >
              <strong>{{ account.label }}</strong>
              <span>{{ account.desc }}</span>
              <span class="demo-meta">{{ account.username }} / {{ account.password }}</span>
            </button>
          </div>
        </div>
      </div>
    </section>
  `
};

const HistoryView = {
  setup() {
    const items = ref([]);
    const searchKeyword = ref("");
    const loading = ref(false);
    const error = ref("");
    const editRatings = reactive({});
    const savingMap = reactive({});
    const successMap = reactive({});
    const errorMap = reactive({});
    const deletingMap = reactive({});

    function hydrateEditRatings(list) {
      Object.keys(editRatings).forEach((key) => delete editRatings[key]);
      Object.keys(savingMap).forEach((key) => delete savingMap[key]);
      Object.keys(successMap).forEach((key) => delete successMap[key]);
      Object.keys(errorMap).forEach((key) => delete errorMap[key]);
      Object.keys(deletingMap).forEach((key) => delete deletingMap[key]);
      list.forEach((item) => {
        editRatings[item.movie_id] = String(item.rating);
      });
    }

    const filteredItems = computed(() => {
      const keyword = searchKeyword.value.trim().toLowerCase();
      if (!keyword) {
        return items.value;
      }
      return items.value.filter((item) => {
        const haystack = `${safeText(item.title)} ${safeText(item.original_title)}`.toLowerCase();
        return haystack.includes(keyword);
      });
    });

    async function loadHistory() {
      if (!appState.user) {
        return;
      }
      loading.value = true;
      error.value = "";
      try {
        const data = await fetchRatingHistory(500);
        items.value = data.items || [];
        hydrateEditRatings(items.value);
      } catch (err) {
        error.value = err.message;
      } finally {
        loading.value = false;
      }
    }

    async function updateHistoryRating(item) {
      const movieId = item.movie_id;
      savingMap[movieId] = true;
      successMap[movieId] = "";
      errorMap[movieId] = "";
      try {
        const data = await saveMyRating(movieId, Number(editRatings[movieId]));
        item.rating = data.rating;
        item.rating_timestamp = data.rating_timestamp;
        editRatings[movieId] = String(data.rating);
        successMap[movieId] = `已更新为 ${data.rating}/5`;
      } catch (err) {
        errorMap[movieId] = err.message;
      } finally {
        savingMap[movieId] = false;
      }
    }

    async function removeHistoryRating(item) {
      const movieId = item.movie_id;
      deletingMap[movieId] = true;
      successMap[movieId] = "";
      errorMap[movieId] = "";
      try {
        await deleteMyRating(movieId);
        items.value = items.value.filter((row) => row.movie_id !== movieId);
        delete editRatings[movieId];
        delete savingMap[movieId];
        delete successMap[movieId];
        delete errorMap[movieId];
        delete deletingMap[movieId];
      } catch (err) {
        errorMap[movieId] = err.message;
      } finally {
        deletingMap[movieId] = false;
      }
    }

    onMounted(loadHistory);
    watch(() => appState.user && appState.user.username, loadHistory);

    return {
      appState,
      items,
      filteredItems,
      searchKeyword,
      loading,
      error,
      loadHistory,
      editRatings,
      savingMap,
      successMap,
      errorMap,
      deletingMap,
      updateHistoryRating,
      removeHistoryRating
    };
  },
  template: `
    <section class="section-shell">
      <div v-if="!appState.user" class="panel panel-inner empty-state">
        <h2 class="small-title">还没有登录</h2>
        <p class="subtitle">登录后才能查看你的评分历史记录。</p>
        <router-link class="button-primary" to="/login">去登录</router-link>
      </div>

      <template v-else>
        <div class="section-heading">
          <div>
            <p class="eyebrow">历史记录</p>
            <h1 class="small-title">{{ appState.user.display_name }} 的评分历史</h1>
          </div>
          <button class="button-secondary" @click="loadHistory">刷新</button>
        </div>

        <div class="panel panel-inner stack">
          <div>
            <p class="eyebrow">搜索</p>
            <h2 class="small-title">按电影名称筛选评分记录</h2>
          </div>
          <input v-model="searchKeyword" class="search-input" placeholder="输入中文名或英文名" />
          <div class="result-count">当前显示 {{ filteredItems.length }} 条记录</div>
        </div>

        <div v-if="loading" class="panel panel-inner">正在加载评分历史...</div>
        <div v-else-if="error" class="panel panel-inner error-box">{{ error }}</div>
        <div v-else-if="items.length === 0" class="panel panel-inner empty-state">
          <p class="subtitle">还没有评分记录，去电影详情页打个分吧。</p>
        </div>
        <div v-else-if="filteredItems.length === 0" class="panel panel-inner empty-state">
          <p class="subtitle">没有找到匹配的评分记录。</p>
        </div>
        <div v-else class="history-list">
          <div v-for="item in filteredItems" :key="item.movie_id" class="panel history-row">
            <router-link :to="'/movie/' + item.movie_id" class="history-poster-link">
              <img class="history-poster" :src="item.poster_url" :alt="item.title" />
            </router-link>
            <div class="history-content">
              <div class="history-top">
                <div>
                  <router-link :to="'/movie/' + item.movie_id" class="history-title">{{ item.title }}</router-link>
                  <p class="movie-original">{{ item.original_title }}</p>
                </div>
                <span class="user-rating-badge">我的评分 {{ item.rating }}/5</span>
              </div>
              <div class="movie-meta">
                <span v-for="genre in item.genres.slice(0, 3)" :key="genre" class="meta-pill">{{ genre }}</span>
              </div>
              <div class="history-edit-row">
                <select v-model="editRatings[item.movie_id]" class="select-input history-rating-select">
                  <option value="0.5">0.5</option>
                  <option value="1.0">1.0</option>
                  <option value="1.5">1.5</option>
                  <option value="2.0">2.0</option>
                  <option value="2.5">2.5</option>
                  <option value="3.0">3.0</option>
                  <option value="3.5">3.5</option>
                  <option value="4.0">4.0</option>
                  <option value="4.5">4.5</option>
                  <option value="5.0">5.0</option>
                </select>
                <button class="button-secondary history-save-button" @click="updateHistoryRating(item)" :disabled="savingMap[item.movie_id]">
                  {{ savingMap[item.movie_id] ? '保存中...' : '修改评分' }}
                </button>
                <button class="button-danger history-delete-button" @click="removeHistoryRating(item)" :disabled="deletingMap[item.movie_id]">
                  {{ deletingMap[item.movie_id] ? '删除中...' : '删除记录' }}
                </button>
              </div>
              <div v-if="successMap[item.movie_id]" class="success-box compact-feedback">{{ successMap[item.movie_id] }}</div>
              <div v-if="errorMap[item.movie_id]" class="error-box compact-feedback">{{ errorMap[item.movie_id] }}</div>
            </div>
          </div>
        </div>
      </template>
    </section>
  `
};

const MovieDetailView = {
  setup() {
    const route = useRoute();
    const movie = computed(() => getMovieById(route.params.id));
    const ratingValue = ref("4.0");
    const currentRating = ref(null);
    const loadingRating = ref(false);
    const savingRating = ref(false);
    const actionMessage = ref("");
    const actionError = ref("");

    async function loadCurrentRating() {
      currentRating.value = null;
      actionError.value = "";
      actionMessage.value = "";
      if (!appState.user || !movie.value) {
        return;
      }
      loadingRating.value = true;
      try {
        const data = await fetchMyRating(movie.value.id);
        currentRating.value = data.rating;
        if (data.rating !== null && data.rating !== undefined) {
          ratingValue.value = String(data.rating);
        }
      } catch (err) {
        actionError.value = err.message;
      } finally {
        loadingRating.value = false;
      }
    }

    async function submitRating() {
      if (!movie.value) {
        return;
      }
      savingRating.value = true;
      actionError.value = "";
      actionMessage.value = "";
      try {
        const data = await saveMyRating(movie.value.id, Number(ratingValue.value));
        currentRating.value = data.rating;
        actionMessage.value = `评分已保存：${data.rating}/5`;
      } catch (err) {
        actionError.value = err.message;
      } finally {
        savingRating.value = false;
      }
    }

    onMounted(loadCurrentRating);
    watch(() => route.params.id, loadCurrentRating);
    watch(() => appState.user && appState.user.username, loadCurrentRating);

    return {
      appState,
      movie,
      formatMovieScore,
      ratingValue,
      currentRating,
      loadingRating,
      savingRating,
      actionMessage,
      actionError,
      submitRating,
      getDefaultReason
    };
  },
  template: `
    <section v-if="!appState.loaded" class="section-shell">
      <div class="panel panel-inner">正在加载电影数据...</div>
    </section>
    <section v-else-if="appState.error" class="section-shell">
      <div class="panel panel-inner">{{ appState.error }}</div>
    </section>
    <section v-else-if="movie" class="section-shell">
      <div class="detail-layout">
        <img class="poster-xl" :src="movie.poster" :alt="movie.title" />
        <div class="stack">
          <div class="panel panel-inner stack">
            <div>
              <p class="eyebrow">详情</p>
              <h1 class="title">{{ movie.title }}</h1>
              <p class="subtitle">{{ movie.originalTitle }} · {{ movie.year }}</p>
            </div>
            <div class="meta-row">
              <span v-for="genre in movie.genres" :key="genre" class="meta-pill">{{ genre }}</span>
              <span class="meta-pill">TMDb 评分 {{ formatMovieScore(movie.score) }}/10</span>
            </div>
            <div class="content-block">
              <div class="block-title">剧情简介</div>
              <p class="subtitle">{{ movie.overview }}</p>
            </div>
          </div>

          <div class="panel panel-inner stack">
            <div class="block-title">推荐视角</div>
            <p class="subtitle">{{ getDefaultReason(movie) }}</p>
          </div>

          <div class="panel panel-inner stack">
            <div class="block-title">我的评分</div>
            <div v-if="!appState.user" class="auth-tip">
              登录后可以给这部电影打分。<router-link to="/login">去登录</router-link>
            </div>
            <template v-else>
              <div class="rating-row">
                <select v-model="ratingValue" class="select-input rating-select">
                  <option value="0.5">0.5</option>
                  <option value="1.0">1.0</option>
                  <option value="1.5">1.5</option>
                  <option value="2.0">2.0</option>
                  <option value="2.5">2.5</option>
                  <option value="3.0">3.0</option>
                  <option value="3.5">3.5</option>
                  <option value="4.0">4.0</option>
                  <option value="4.5">4.5</option>
                  <option value="5.0">5.0</option>
                </select>
                <button class="button-primary" @click="submitRating" :disabled="savingRating">
                  {{ savingRating ? '保存中...' : '提交评分' }}
                </button>
              </div>
              <div v-if="loadingRating" class="muted">正在读取你的历史评分...</div>
              <div v-else-if="currentRating !== null" class="success-box">你当前给这部电影的评分是 {{ currentRating }}/5</div>
              <div v-else class="muted">你还没有给这部电影评分。</div>
              <div v-if="actionMessage" class="success-box">{{ actionMessage }}</div>
              <div v-if="actionError" class="error-box">{{ actionError }}</div>
            </template>
          </div>
        </div>
      </div>
    </section>
    <section v-else class="section-shell">
      <div class="panel panel-inner">
        <h2 class="small-title">没有找到这部电影</h2>
      </div>
    </section>
  `
};

const RecommendView = {
  components: { MovieCard },
  setup() {
    const loading = ref(false);
    const error = ref("");
    const popularItems = ref([]);
    const itemcfItems = ref([]);
    const twotowerItems = ref([]);
    const mergedRawItems = ref([]);
    const mergedItems = ref([]);
    const positiveSeedCount = ref(0);
    const twotowerPositiveCount = ref(0);
    const rerankMeta = ref(null);
    const batchMessage = ref("");

    function getExposureStorageKey() {
      const username = appState.user && appState.user.username ? appState.user.username : "guest";
      return `movie_rec_exposed_${username}`;
    }

    function getCacheStorageKey() {
      const username = appState.user && appState.user.username ? appState.user.username : "guest";
      return `movie_rec_cache_${username}`;
    }

    function readExposureIds() {
      try {
        const raw = sessionStorage.getItem(getExposureStorageKey());
        if (!raw) {
          return [];
        }
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) {
          return [];
        }
        return parsed.map((id) => Number(id)).filter((id) => Number.isInteger(id) && id > 0);
      } catch (error) {
        return [];
      }
    }

    function writeExposureIds(ids) {
      sessionStorage.setItem(getExposureStorageKey(), JSON.stringify(Array.from(new Set(ids))));
    }

    function readCacheMovies() {
      try {
        const raw = sessionStorage.getItem(getCacheStorageKey());
        if (!raw) {
          return [];
        }
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
      } catch (error) {
        return [];
      }
    }

    function writeCacheMovies(items) {
      sessionStorage.setItem(getCacheStorageKey(), JSON.stringify(Array.isArray(items) ? items : []));
    }

    function resetExposureIds() {
      sessionStorage.removeItem(getExposureStorageKey());
    }

    function resetCacheMovies() {
      sessionStorage.removeItem(getCacheStorageKey());
    }

    function appendExposureIds(ids) {
      const merged = [...readExposureIds(), ...ids];
      writeExposureIds(merged);
    }

    async function loadRecommendations(options = {}) {
      const resetExposure = !!options.resetExposure;
      const useNextBatch = !!options.useNextBatch;
      if (resetExposure) {
        resetExposureIds();
        resetCacheMovies();
      }
      if (useNextBatch && appState.user) {
        const cachedMovies = readCacheMovies();
        if (cachedMovies.length > 0) {
          const nextBatch = cachedMovies.slice(0, 20).map(normalizeRecommendationMovie);
          const remainingCache = cachedMovies.slice(20);
          mergedRawItems.value = nextBatch;
          mergedItems.value = nextBatch;
          writeCacheMovies(remainingCache);
          appendExposureIds(nextBatch.map((movie) => movie.id));
          rerankMeta.value = {
            ...(rerankMeta.value || {}),
            cached: true,
            cache_served_count: nextBatch.length,
            cache_size_remaining: remainingCache.length
          };
          batchMessage.value = remainingCache.length > 0
            ? `本次优先使用缓存召回，还剩 ${remainingCache.length} 个缓存候选。`
            : "本次优先使用缓存召回，缓存已取完，下次会重新请求后端。";
          return;
        }
      }
      const excludeIds = useNextBatch ? readExposureIds() : [];
      loading.value = true;
      error.value = "";
      batchMessage.value = "";
      try {
        const popularData = await fetchPopularRecommendations(20, excludeIds);
        popularItems.value = (popularData.items || []).map(normalizeRecommendationMovie);

        if (appState.user) {
          const data = await fetchMyRecommendations(20, excludeIds);
          popularItems.value = (data.popular || []).map(normalizeRecommendationMovie);
          itemcfItems.value = (data.itemcf || []).map(normalizeRecommendationMovie);
          twotowerItems.value = (data.twotower || []).map(normalizeRecommendationMovie);
          mergedRawItems.value = (data.merged_raw || []).map(normalizeRecommendationMovie);
          mergedItems.value = (data.merged || []).map(normalizeRecommendationMovie);
          positiveSeedCount.value = data.positive_seed_count || 0;
          twotowerPositiveCount.value = data.twotower_positive_count || 0;
          rerankMeta.value = data.rerank_meta || null;
          writeCacheMovies(data.cache_candidates || []);
          if (mergedItems.value.length > 0) {
            appendExposureIds(mergedItems.value.map((movie) => movie.id));
          } else if (useNextBatch) {
            batchMessage.value = "当前没有更多新推荐了，可以稍后再试。";
          }
        } else {
          itemcfItems.value = [];
          twotowerItems.value = [];
          mergedRawItems.value = [];
          mergedItems.value = [];
          positiveSeedCount.value = 0;
          twotowerPositiveCount.value = 0;
          rerankMeta.value = null;
          resetCacheMovies();
          if (popularItems.value.length > 0) {
            appendExposureIds(popularItems.value.map((movie) => movie.id));
          } else if (useNextBatch) {
            batchMessage.value = "当前没有更多新推荐了，可以稍后再试。";
          }
        }
      } catch (err) {
        error.value = err.message;
      } finally {
        loading.value = false;
      }
    }

    function refreshRecommendations() {
      loadRecommendations({ resetExposure: true });
    }

    function loadNextBatch() {
      loadRecommendations({ useNextBatch: true });
    }

    onMounted(() => loadRecommendations({ resetExposure: true }));
    watch(() => appState.user && appState.user.username, () => loadRecommendations({ resetExposure: true }));

    return {
      appState,
      loading,
      error,
      popularItems,
      itemcfItems,
      twotowerItems,
      mergedRawItems,
      mergedItems,
      positiveSeedCount,
      twotowerPositiveCount,
      rerankMeta,
      batchMessage,
      loadRecommendations,
      refreshRecommendations,
      loadNextBatch
    };
  },
  template: `
    <section class="section-shell">
      <div v-if="!appState.loaded" class="panel panel-inner">姝ｅ湪鍔犺浇鐢靛奖鏁版嵁...</div>
      <div v-else-if="appState.error" class="panel panel-inner">{{ appState.error }}</div>
      <template v-else>
        <div class="panel panel-inner stack">
          <div class="section-heading">
            <div>
              <p class="eyebrow">鎺ㄨ崘</p>
              <h1 class="small-title">褰撳墠鍙洖缁撴灉</h1>
            </div>
            <button class="button-secondary" @click="loadRecommendations">鍒锋柊鍙洖</button>
          </div>
          <p v-if="!appState.user" class="subtitle">褰撳墠灞曠ず鐨勬槸鐑棬鍙洖銆傜櫥褰曞悗鍙互鐪嬪埌鍩轰簬浣犵殑楂樺垎鍘嗗彶鐢熸垚鐨?ItemCF 鍙洖鍜岀粍鍚堝彫鍥炪€?/p>
          <p v-else class="subtitle">褰撳墠宸茬粡鎺ュ叆鐑棬鍙洖銆両temCF 鍙洖鍜屽弻濉斿彫鍥炪€傜紦瀛樺彫鍥炴垜浠悗闈㈠啀缁х画琛ャ€?/p>
          <div v-if="appState.user" class="movie-meta">
            <span class="meta-pill">楂樺垎绉嶅瓙鏁?{{ positiveSeedCount }}</span>
            <span class="meta-pill">鍙屽璁粌鏍锋湰鏁?{{ twotowerPositiveCount }}</span>
            <span class="meta-pill">鐑棬閫氶亾宸插惎鐢?/span>
            <span class="meta-pill">ItemCF 閫氶亾宸插惎鐢?/span>
            <span class="meta-pill">鍙屽閫氶亾宸插惎鐢?/span>
          </div>
        </div>

        <div v-if="loading" class="panel panel-inner">姝ｅ湪鐢熸垚鎺ㄨ崘缁撴灉...</div>
        <div v-else-if="error" class="panel panel-inner error-box">{{ error }}</div>
        <template v-else>
          <div v-if="appState.user && mergedItems.length > 0" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">缁勫悎鍙洖</p>
                <h2 class="small-title">鍏堢湅杩欑粍鏈€缁堝€欓€?/h2>
              </div>
            </div>
            <div class="movie-grid">
              <MovieCard v-for="movie in mergedItems" :key="'merged-' + movie.id" :movie="movie" />
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">ItemCF</p>
                <h2 class="small-title">鍩轰簬浣犵殑楂樺垎鍘嗗彶</h2>
              </div>
            </div>
            <div v-if="itemcfItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in itemcfItems" :key="'itemcf-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">浣犵殑楂樺垎绉嶅瓙杩樹笉澶熴€傚厛鍘荤粰鍑犻儴鐢靛奖鎵撳埌 4 鍒嗘垨 5 鍒嗭紝ItemCF 鍙洖灏变細鏇村儚鏍枫€?/p>
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">鍙屽鍙洖</p>
                <h2 class="small-title">鍚戦噺鍖归厤寰楀埌鐨勫€欓€?/h2>
              </div>
            </div>
            <div v-if="twotowerItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in twotowerItems" :key="'twotower-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">褰撳墠鐢ㄦ埛杩樻病鏈夊舰鎴愬彲鐢ㄧ殑鍙屽鍚戦噺锛屾垨鑰呭弻濉旂储寮曞皻鏈瀯寤恒€?/p>
            </div>
          </div>

          <div class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">鐑棬鍙洖</p>
                <h2 class="small-title">鍏ㄥ眬鍏滃簳鍊欓€?/h2>
              </div>
            </div>
            <div class="movie-grid">
              <MovieCard v-for="movie in popularItems" :key="'popular-' + movie.id" :movie="movie" />
            </div>
          </div>
        </template>
      </template>
    </section>
  `
};

const RecommendViewV2 = {
  components: { MovieCard },
  setup() {
    const loading = ref(false);
    const error = ref("");
    const popularItems = ref([]);
    const itemcfItems = ref([]);
    const twotowerItems = ref([]);
    const mergedRawItems = ref([]);
    const mergedItems = ref([]);
    const positiveSeedCount = ref(0);
    const twotowerPositiveCount = ref(0);
    const rerankMeta = ref(null);
    const batchMessage = ref("");

    function getExposureStorageKey() {
      const username = appState.user && appState.user.username ? appState.user.username : "guest";
      return `movie_rec_exposed_${username}`;
    }

    function readExposureIds() {
      try {
        const raw = sessionStorage.getItem(getExposureStorageKey());
        if (!raw) {
          return [];
        }
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) {
          return [];
        }
        return parsed.map((id) => Number(id)).filter((id) => Number.isInteger(id) && id > 0);
      } catch (error) {
        return [];
      }
    }

    function writeExposureIds(ids) {
      sessionStorage.setItem(getExposureStorageKey(), JSON.stringify(Array.from(new Set(ids))));
    }

    function resetExposureIds() {
      sessionStorage.removeItem(getExposureStorageKey());
    }

    function appendExposureIds(ids) {
      writeExposureIds([...readExposureIds(), ...ids]);
    }

    async function loadRecommendations(options = {}) {
      const resetExposure = !!options.resetExposure;
      const useNextBatch = !!options.useNextBatch;
      if (resetExposure) {
        resetExposureIds();
      }
      const excludeIds = useNextBatch ? readExposureIds() : [];
      loading.value = true;
      error.value = "";
      batchMessage.value = "";
      try {
        const popularData = await fetchPopularRecommendations(20, excludeIds);
        popularItems.value = (popularData.items || []).map(normalizeRecommendationMovie);

        if (appState.user) {
          const data = await fetchMyRecommendations(20, excludeIds);
          popularItems.value = (data.popular || []).map(normalizeRecommendationMovie);
          itemcfItems.value = (data.itemcf || []).map(normalizeRecommendationMovie);
          twotowerItems.value = (data.twotower || []).map(normalizeRecommendationMovie);
          mergedRawItems.value = (data.merged_raw || []).map(normalizeRecommendationMovie);
          mergedItems.value = (data.merged || []).map(normalizeRecommendationMovie);
          positiveSeedCount.value = data.positive_seed_count || 0;
          twotowerPositiveCount.value = data.twotower_positive_count || 0;
          rerankMeta.value = data.rerank_meta || null;
          if (mergedItems.value.length > 0) {
            appendExposureIds(mergedItems.value.map((movie) => movie.id));
          } else if (useNextBatch) {
            batchMessage.value = "当前没有更多新推荐了，可以稍后再试。";
          }
        } else {
          itemcfItems.value = [];
          twotowerItems.value = [];
          mergedRawItems.value = [];
          mergedItems.value = [];
          positiveSeedCount.value = 0;
          twotowerPositiveCount.value = 0;
          rerankMeta.value = null;
          if (popularItems.value.length > 0) {
            appendExposureIds(popularItems.value.map((movie) => movie.id));
          } else if (useNextBatch) {
            batchMessage.value = "当前没有更多新推荐了，可以稍后再试。";
          }
        }
      } catch (err) {
        error.value = err.message;
      } finally {
        loading.value = false;
      }
    }

    function refreshRecommendations() {
      loadRecommendations({ resetExposure: true });
    }

    function loadNextBatch() {
      loadRecommendations({ useNextBatch: true });
    }

    onMounted(() => loadRecommendations({ resetExposure: true }));
    watch(() => appState.user && appState.user.username, () => loadRecommendations({ resetExposure: true }));

    return {
      appState,
      loading,
      error,
      popularItems,
      itemcfItems,
      twotowerItems,
      mergedRawItems,
      mergedItems,
      positiveSeedCount,
      twotowerPositiveCount,
      rerankMeta,
      batchMessage,
      loadRecommendations,
      refreshRecommendations,
      loadNextBatch
    };
  },
  template: `
    <section class="section-shell">
      <div v-if="!appState.loaded" class="panel panel-inner">姝ｅ湪鍔犺浇鐢靛奖鏁版嵁...</div>
      <div v-else-if="appState.error" class="panel panel-inner">{{ appState.error }}</div>
      <template v-else>
        <div class="panel panel-inner stack">
          <div class="section-heading">
            <div>
              <p class="eyebrow">鎺ㄨ崘</p>
              <h1 class="small-title">褰撳墠鍙洖缁撴灉</h1>
            </div>
            <button class="button-secondary" @click="loadRecommendations">鍒锋柊鎺ㄨ崘</button>
          </div>
          <p v-if="!appState.user" class="subtitle">褰撳墠灞曠ず鐨勬槸鐑棬鍙洖銆傜櫥褰曞悗鍙互鐪嬪埌 ItemCF銆佸弻濉斿彫鍥烇紝浠ュ強 LightGBM + MMR 鐨勬渶缁堢粨鏋溿€?/p>
          <p v-else class="subtitle">鐜板湪杩欓〉浼氭妸鍙洖銆佺簿鎺掋€侀噸鎺掑垎寮€缁欎綘鐪嬶紝鏂逛究浣犵洿鎺ヨ瀵熸瘡涓€姝ュ湪鍋氫粈涔堛€?/p>
          <div v-if="appState.user" class="movie-meta">
            <span class="meta-pill">楂樺垎绉嶅瓙 {{ positiveSeedCount }}</span>
            <span class="meta-pill">鍙屽璁粌鏍锋湰 {{ twotowerPositiveCount }}</span>
            <span class="meta-pill">鐑棬閫氶亾宸插惎鐢?/span>
            <span class="meta-pill">ItemCF 閫氶亾宸插惎鐢?/span>
            <span class="meta-pill">鍙屽閫氶亾宸插惎鐢?/span>
          </div>
        </div>

        <div v-if="loading" class="panel panel-inner">姝ｅ湪鐢熸垚鎺ㄨ崘缁撴灉...</div>
        <div v-else-if="error" class="panel panel-inner error-box">{{ error }}</div>
        <template v-else>
          <div v-if="appState.user && mergedItems.length > 0" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">缁勫悎鎺ㄨ崘</p>
                <h2 class="small-title">绮炬帓鍓嶅悗瀵圭収</h2>
              </div>
            </div>
            <div v-if="rerankMeta" class="movie-meta">
              <span class="meta-pill">鍊欓€夋睜 {{ rerankMeta.candidate_count || mergedRawItems.length }}</span>
              <span class="meta-pill">MMR 绐楀彛 {{ rerankMeta.window_size || 0 }}</span>
              <span class="meta-pill">alpha {{ rerankMeta.alpha ?? '--' }}</span>
              <span class="meta-pill">{{ rerankMeta.model_loaded ? 'LightGBM 已加载' : 'LightGBM 未加载' }}</span>
            </div>
            <div v-if="rerankMeta" class="movie-meta">
              <span class="meta-pill">缁偓甯撴稉濠囨 {{ rerankMeta.merged_candidate_limit || '--' }}</span>
            </div>
            <div class="compare-grid">
              <div class="panel panel-inner compare-panel">
                <div class="compare-header">
                  <div>
                    <p class="eyebrow">LightGBM 绮炬帓</p>
                    <h3 class="small-title">閲嶆帓鍓?Top 20</h3>
                  </div>
                  <p class="subtitle">鍏堟寜鐩稿叧鎬ф帓搴忥紝鍐嶄氦缁?MMR 鍋氭渶鍚庤皟鏁淬€?/p>
                </div>
                <div class="movie-grid compare-movie-grid">
                  <MovieCard v-for="movie in mergedRawItems" :key="'merged-raw-' + movie.id" :movie="movie" />
                </div>
              </div>

              <div class="panel panel-inner compare-panel">
                <div class="compare-header">
                  <div>
                    <p class="eyebrow">婊戝姩绐楀彛 MMR</p>
                    <h3 class="small-title">鏈€缁?Top 20</h3>
                  </div>
                  <p class="subtitle">鍦ㄧ浉鍏虫€у敖閲忎笉鎺夌殑鎯呭喌涓嬶紝鎶婂唴瀹瑰垎甯冩媺寮€涓€浜涖€?/p>
                </div>
                <div class="movie-grid compare-movie-grid">
                  <MovieCard v-for="movie in mergedItems" :key="'merged-' + movie.id" :movie="movie" />
                </div>
              </div>
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">ItemCF</p>
                <h2 class="small-title">鍩轰簬浣犵殑楂樺垎鍘嗗彶</h2>
              </div>
            </div>
            <div v-if="itemcfItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in itemcfItems" :key="'itemcf-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">浣犵殑楂樺垎绉嶅瓙杩樹笉澶熴€傚厛鍘荤粰鍑犻儴鐢靛奖鎵撳埌 4 鍒嗘垨 5 鍒嗭紝ItemCF 缁撴灉浼氭洿绋冲畾銆?/p>
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">鍙屽鍙洖</p>
                <h2 class="small-title">鍚戦噺鍖归厤寰楀埌鐨勫€欓€?/h2>
              </div>
            </div>
            <div v-if="twotowerItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in twotowerItems" :key="'twotower-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">褰撳墠鐢ㄦ埛杩樻病鏈夊舰鎴愬彲鐢ㄧ殑鍙屽鍚戦噺锛屾垨鑰呭弻濉旂储寮曞皻鏈瀯寤恒€?/p>
            </div>
          </div>

          <div class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">鐑棬鍙洖</p>
                <h2 class="small-title">鍏ㄥ眬鍏滃簳鍊欓€?/h2>
              </div>
            </div>
            <div class="movie-grid">
              <MovieCard v-for="movie in popularItems" :key="'popular-' + movie.id" :movie="movie" />
            </div>
          </div>
        </template>
      </template>
    </section>
  `
};

function setupRecommendViewV4() {
  const loading = ref(false);
  const error = ref("");
  const popularItems = ref([]);
  const genreItems = ref([]);
  const longTailItems = ref([]);
  const itemcfItems = ref([]);
  const twotowerItems = ref([]);
  const mergedRawItems = ref([]);
  const mergedItems = ref([]);
  const positiveSeedCount = ref(0);
  const twotowerPositiveCount = ref(0);
  const rerankMeta = ref(null);
  const batchMessage = ref("");

  function getExposureStorageKey() {
    const username = appState.user && appState.user.username ? appState.user.username : "guest";
    return `movie_rec_exposed_${username}`;
  }

  function readExposureIds() {
    try {
      const raw = sessionStorage.getItem(getExposureStorageKey());
      if (!raw) {
        return [];
      }
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) {
        return [];
      }
      return parsed.map((id) => Number(id)).filter((id) => Number.isInteger(id) && id > 0);
    } catch (error) {
      return [];
    }
  }

  function writeExposureIds(ids) {
    sessionStorage.setItem(getExposureStorageKey(), JSON.stringify(Array.from(new Set(ids))));
  }

  function resetExposureIds() {
    sessionStorage.removeItem(getExposureStorageKey());
  }

  function appendExposureIds(ids) {
    writeExposureIds([...readExposureIds(), ...ids]);
  }

  async function loadRecommendations(options = {}) {
    const resetExposure = !!options.resetExposure;
    const useNextBatch = !!options.useNextBatch;
    if (resetExposure && !appState.user) {
      resetExposureIds();
    }

    const excludeIds = !appState.user && useNextBatch ? readExposureIds() : [];
    loading.value = true;
    error.value = "";
    batchMessage.value = "";

    try {
      const popularData = await fetchPopularRecommendations(20, excludeIds);
      popularItems.value = (popularData.items || []).map(normalizeRecommendationMovie);

      if (appState.user) {
        const data = await fetchMyRecommendations(20, {
          nextBatch: useNextBatch
        });
        popularItems.value = (data.popular || []).map(normalizeRecommendationMovie);
        genreItems.value = (data.genre || []).map(normalizeRecommendationMovie);
        longTailItems.value = (data.long_tail || []).map(normalizeRecommendationMovie);
        itemcfItems.value = (data.itemcf || []).map(normalizeRecommendationMovie);
        twotowerItems.value = (data.twotower || []).map(normalizeRecommendationMovie);
        mergedRawItems.value = (data.merged_raw || []).map(normalizeRecommendationMovie);
        mergedItems.value = (data.merged || []).map(normalizeRecommendationMovie);
        positiveSeedCount.value = data.positive_seed_count || 0;
        twotowerPositiveCount.value = data.twotower_positive_count || 0;
        rerankMeta.value = data.rerank_meta || null;

        if (mergedItems.value.length === 0 && useNextBatch) {
          batchMessage.value = "当前没有更多新推荐了，可以稍后再试。";
        }
      } else {
        genreItems.value = [];
        longTailItems.value = [];
        itemcfItems.value = [];
        twotowerItems.value = [];
        mergedRawItems.value = [];
        mergedItems.value = [];
        positiveSeedCount.value = 0;
        twotowerPositiveCount.value = 0;
        rerankMeta.value = null;

        if (popularItems.value.length > 0) {
          appendExposureIds(popularItems.value.map((movie) => movie.id));
        } else if (useNextBatch) {
          batchMessage.value = "当前没有更多新推荐了，可以稍后再试。";
        }
      }
    } catch (err) {
      error.value = err.message;
    } finally {
      loading.value = false;
    }
  }

  function refreshRecommendations() {
    loadRecommendations({ resetExposure: !appState.user });
  }

  function loadNextBatch() {
    loadRecommendations({ useNextBatch: true });
  }

  onMounted(() => loadRecommendations({ resetExposure: !appState.user }));
  watch(() => appState.user && appState.user.username, () => loadRecommendations({ resetExposure: !appState.user }));

  return {
    appState,
    loading,
    error,
    popularItems,
    genreItems,
    longTailItems,
    itemcfItems,
    twotowerItems,
    mergedRawItems,
    mergedItems,
    positiveSeedCount,
    twotowerPositiveCount,
    rerankMeta,
    batchMessage,
    loadRecommendations,
    refreshRecommendations,
    loadNextBatch
  };
}

const RecommendViewV3 = {
  components: { MovieCard },
  setup() {
    return setupRecommendViewV4();
  },
  template: `
    <section class="section-shell">
      <div v-if="!appState.loaded" class="panel panel-inner">正在加载电影数据...</div>
      <div v-else-if="appState.error" class="panel panel-inner">{{ appState.error }}</div>
      <template v-else>
        <div class="panel panel-inner stack">
          <div class="section-heading">
            <div>
              <p class="eyebrow">推荐</p>
              <h1 class="small-title">当前推荐结果</h1>
            </div>
            <div class="toolbar">
              <router-link class="button-secondary" to="/recommend">推荐流程页</router-link>
              <router-link class="button-secondary" to="/recommend-final">最终推荐页</router-link>
              <button class="button-secondary" @click="loadNextBatch">换一批</button>
            </div>
          </div>
          <div v-if="appState.user" class="movie-meta">
            <span class="meta-pill">高分种子 {{ positiveSeedCount }}</span>
            <span class="meta-pill">双塔训练样本 {{ twotowerPositiveCount }}</span>
            <span class="meta-pill">热门通道</span>
            <span class="meta-pill">类型偏好通道</span>
            <span class="meta-pill">长尾探索通道</span>
            <span class="meta-pill">ItemCF 通道</span>
            <span class="meta-pill">双塔通道</span>
          </div>
        </div>

        <div v-if="loading" class="panel panel-inner">正在生成推荐结果...</div>
        <div v-else-if="error" class="panel panel-inner error-box">{{ error }}</div>
        <template v-else>
          <div v-if="appState.user && rerankMeta" class="panel panel-inner">
            <div class="movie-meta">
              <span class="meta-pill">热门 {{ rerankMeta.popular_raw_count || popularItems.length }}/{{ rerankMeta.channel_limits?.popular || 30 }}</span>
              <span class="meta-pill">类型 {{ rerankMeta.genre_raw_count || genreItems.length }}/{{ rerankMeta.channel_limits?.genre || 60 }}</span>
              <span class="meta-pill">长尾 {{ rerankMeta.long_tail_raw_count || longTailItems.length }}/{{ rerankMeta.channel_limits?.long_tail || 20 }}</span>
              <span class="meta-pill">ItemCF {{ rerankMeta.itemcf_raw_count || itemcfItems.length }}/{{ rerankMeta.channel_limits?.itemcf || 80 }}</span>
              <span class="meta-pill">双塔 {{ rerankMeta.twotower_raw_count || twotowerItems.length }}/{{ rerankMeta.channel_limits?.twotower || 80 }}</span>
            </div>
          </div>

          <div v-if="batchMessage" class="panel panel-inner success-box">{{ batchMessage }}</div>

          <div v-if="appState.user && mergedItems.length > 0" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">组合推荐</p>
                <h2 class="small-title">精排前后对照</h2>
              </div>
            </div>
            <div v-if="rerankMeta" class="movie-meta">
              <span class="meta-pill">候选池 {{ rerankMeta.candidate_count || mergedRawItems.length }}</span>
              <span class="meta-pill">已过滤 {{ rerankMeta.excluded_count || 0 }}</span>
              <span class="meta-pill">MMR 窗口 {{ rerankMeta.window_size || 0 }}</span>
              <span class="meta-pill">alpha {{ rerankMeta.alpha ?? '--' }}</span>
              <span class="meta-pill">{{ rerankMeta.model_loaded ? 'LightGBM 已加载' : 'LightGBM 未加载' }}</span>
            </div>
            <div class="compare-grid">
              <div class="panel panel-inner compare-panel">
                <div class="compare-header">
                  <div>
                    <p class="eyebrow">LightGBM 精排</p>
                    <h3 class="small-title">重排前 Top 20</h3>
                  </div>
                </div>
                <div class="movie-grid compare-movie-grid">
                  <MovieCard v-for="movie in mergedRawItems" :key="'merged-raw-v3-' + movie.id" :movie="movie" />
                </div>
              </div>

              <div class="panel panel-inner compare-panel">
                <div class="compare-header">
                  <div>
                    <p class="eyebrow">滑动窗口 MMR</p>
                    <h3 class="small-title">最终 Top 20</h3>
                  </div>
                </div>
                <div class="movie-grid compare-movie-grid">
                  <MovieCard v-for="movie in mergedItems" :key="'merged-v3-' + movie.id" :movie="movie" />
                </div>
              </div>
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">类型偏好召回</p>
                <h2 class="small-title">基于你高分历史里的偏好类型</h2>
              </div>
            </div>
            <div v-if="genreItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in genreItems" :key="'genre-v3-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">当前没有可展示的类型偏好候选。</p>
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">长尾探索召回</p>
                <h2 class="small-title">在偏好类型里补充不那么热门的电影</h2>
              </div>
            </div>
            <div v-if="longTailItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in longTailItems" :key="'long-tail-v3-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">当前没有可展示的长尾探索候选。</p>
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">ItemCF</p>
                <h2 class="small-title">基于你的高分历史</h2>
              </div>
            </div>
            <div v-if="itemcfItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in itemcfItems" :key="'itemcf-v3-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">当前没有可展示的 ItemCF 候选。</p>
            </div>
          </div>

          <div v-if="appState.user" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">双塔召回</p>
                <h2 class="small-title">向量匹配候选</h2>
              </div>
            </div>
            <div v-if="twotowerItems.length > 0" class="movie-grid">
              <MovieCard v-for="movie in twotowerItems" :key="'twotower-v3-' + movie.id" :movie="movie" />
            </div>
            <div v-else class="panel panel-inner empty-state">
              <p class="subtitle">当前没有可展示的双塔候选。</p>
            </div>
          </div>

          <div class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">热门召回</p>
                <h2 class="small-title">全局兜底候选</h2>
              </div>
            </div>
            <div class="movie-grid">
              <MovieCard v-for="movie in popularItems" :key="'popular-v3-' + movie.id" :movie="movie" />
            </div>
          </div>
        </template>
      </template>
    </section>
  `
};

const FinalRecommendView = {
  components: { MovieCard },
  setup() {
    const state = setupRecommendViewV4();
    const displayItems = computed(() => {
      if (state.appState.user) {
        return state.mergedItems.value;
      }
      return state.popularItems.value;
    });

    return {
      ...state,
      displayItems
    };
  },
  template: `
    <section class="section-shell">
      <div v-if="!appState.loaded" class="panel panel-inner">正在加载电影数据...</div>
      <div v-else-if="appState.error" class="panel panel-inner">{{ appState.error }}</div>
      <template v-else>
        <div class="panel panel-inner stack">
          <div class="section-heading">
            <div>
              <p class="eyebrow">推荐</p>
              <h1 class="small-title">最终推荐结果</h1>
            </div>
            <div class="toolbar">
              <router-link class="button-secondary" to="/recommend">推荐流程页</router-link>
              <router-link class="button-secondary" to="/recommend-final">最终推荐页</router-link>
              <button class="button-secondary" @click="loadNextBatch">换一批</button>
            </div>
          </div>
          <p v-if="appState.user" class="subtitle">这里直接展示精排和重排后的最终结果，不再展开各召回通道。</p>
          <p v-else class="subtitle">当前未登录，先展示热门兜底结果。登录后这里会展示个性化最终推荐。</p>
          <div v-if="appState.user && rerankMeta" class="movie-meta">
            <span class="meta-pill">最终返回 {{ displayItems.length }}</span>
            <span class="meta-pill">候选池 {{ rerankMeta.candidate_count || mergedRawItems.length }}</span>
            <span class="meta-pill">已过滤 {{ rerankMeta.excluded_count || 0 }}</span>
            <span class="meta-pill">MMR 窗口 {{ rerankMeta.window_size || 0 }}</span>
          </div>
        </div>

        <div v-if="loading" class="panel panel-inner">正在生成推荐结果...</div>
        <div v-else-if="error" class="panel panel-inner error-box">{{ error }}</div>
        <template v-else>
          <div v-if="batchMessage" class="panel panel-inner success-box">{{ batchMessage }}</div>

          <div v-if="displayItems.length > 0" class="stack">
            <div class="section-heading">
              <div>
                <p class="eyebrow">{{ appState.user ? '最终结果' : '热门结果' }}</p>
                <h2 class="small-title">{{ appState.user ? '给用户实际展示的 Top 20' : '当前可展示的热门电影' }}</h2>
              </div>
            </div>
            <div class="movie-grid">
              <MovieCard v-for="movie in displayItems" :key="'final-' + movie.id" :movie="movie" />
            </div>
          </div>

          <div v-else class="panel panel-inner empty-state">
            <p class="subtitle">当前没有可展示的推荐结果。</p>
          </div>
        </template>
      </template>
    </section>
  `
};

const routes = [
  { path: "/", component: HomeView },
  { path: "/login", component: LoginView },
  { path: "/history", component: HistoryView },
  { path: "/search", component: SearchView },
  { path: "/movie/:id", component: MovieDetailView },
  { path: "/recommend", component: RecommendViewV3 },
  { path: "/recommend-final", component: FinalRecommendView }
];

const router = createRouter({
  history: createWebHashHistory(),
  routes
});

const App = {
  setup() {
    function logout() {
      clearSession();
      router.push("/");
    }

    return { appState, logout };
  },
  template: `
    <div class="app-shell">
      <div class="background-shape background-shape-left"></div>
      <div class="background-shape background-shape-right"></div>

      <header class="site-header panel">
        <router-link class="brand-link" to="/">
          <span class="brand-mark">影</span>
          <div>
            <div class="brand-title">Movie Recommendation</div>
            <div class="brand-subtitle">Find your next movie</div>
          </div>
        </router-link>

        <div class="header-actions">
          <nav class="nav-links">
            <router-link to="/">首页</router-link>
            <router-link to="/search">搜索</router-link>
            <router-link to="/recommend">推荐流程</router-link>
            <router-link to="/recommend-final">最终推荐</router-link>
            <router-link to="/history">历史</router-link>
            <router-link to="/login">{{ appState.user ? "账户" : "登录" }}</router-link>
          </nav>
          <div v-if="appState.user" class="user-chip">
            <span>{{ appState.user.display_name }}</span>
            <button class="text-button" @click="logout">退出</button>
          </div>
        </div>
      </header>

      <main class="app-main">
        <router-view></router-view>
      </main>
    </div>
  `
};

createApp(App).use(router).mount("#app");
loadMovies();
loadCatalogStats();
validateSession();
